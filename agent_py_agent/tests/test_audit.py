# LLM: 审计原始事实与索引均在临时owner下，读取不得把损坏来源转成成功空值。
# 模块用途: 验证原审计合同和带覆盖证明的索引读取。
"""审计日志测试。"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.audit import AuditAction, AuditLogger, AuditQuery
from agent_py_agent.agent.audit.logger import AuditStatus, LogParams

_AUDIT_QUERY_ROWS = (
    ("audit_1", 100, "CREATE_TASK", "alice", "chat", "task-1", "success"),
    ("audit_2", 50, "DISPATCH", "alice", "chat", "task-1", "success"),
    ("audit_3", 30, "UPDATE_TASK", "bob", "feishu", "task-2", "success"),
    ("audit_4", 10, "QUERY", "bob", "chat", "task-1", "denied"),
)


def _write_audit_query_data(audit_path: Path, audit_file: Path) -> None:
    """Write standard audit query JSONL fixture."""
    audit_path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with open(audit_file, "w", encoding="utf-8") as f:
        for entry_id, offset, action, user_id, channel, target_id, status in _AUDIT_QUERY_ROWS:
            entry = {
                "entry_id": entry_id,
                "timestamp": now - offset,
                "action": action,
                "user_id": user_id,
                "channel": channel,
                "target_type": "task",
                "target_id": target_id,
                "status": status,
                "details": {},
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="test-user",
                channel="chat",
                target_type="task",
                target_id="task-123",
            )
        )

        self.assertTrue(self.audit_path.exists())
        self.assertTrue((self.audit_path / "audit.jsonl").exists())
        self.assertEqual(entry.action, "CREATE_TASK")

    def test_disabled_audit_does_not_write_file(self):
        """audit_enabled=false 时返回事件对象，但不落盘。"""
        class MockConfig:
            audit_log_path = str(self.audit_path)
            audit_enabled = False

        logger = AuditLogger(MockConfig())
        entry = logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="test-user",
                channel="chat",
                target_type="task",
                target_id="task-123",
            )
        )

        self.assertEqual(entry.action, "CREATE_TASK")
        self.assertFalse((self.audit_path / "audit.jsonl").exists())

    def test_audit_string_off_is_not_a_machine_disable_value(self):
        """审计开关只接受明确 false/0；off/no 不再作为兜底禁用值。"""
        class MockConfig:
            audit_log_path = str(self.audit_path)
            audit_enabled = "off"

        logger = AuditLogger(MockConfig())
        logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="test-user",
                channel="chat",
                target_type="task",
                target_id="task-123",
            )
        )

        self.assertTrue((self.audit_path / "audit.jsonl").exists())

    def test_log_writes_jsonl(self):
        """测试写入 JSONL 格式。"""
        logger = AuditLogger(self.config)

        entry1 = logger.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id="user-1",
                channel="chat",
                target_type="task",
                target_id="task-1",
            )
        )

        entry2 = logger.log(
            LogParams(
                action=AuditAction.DISPATCH,
                user_id="user-2",
                channel="feishu",
                target_type="task",
                target_id="task-2",
            )
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
            LogParams(
                action=AuditAction.UPDATE_TASK,
                user_id="test-user",
                channel="chat",
                target_type="task",
                target_id="task-456",
                details={"status_before": "pending", "status_after": "running"},
            )
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


# LLM: 显式绑定测试owner根，索引不通过审计文件父目录猜测身份。
# 类用途: 验证原审计查询、分页、聚合和清理合同。
class TestAuditQuery(unittest.TestCase):
    """审计查询测试。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_path = Path(self.temp_dir) / "audit"
        self.audit_file = self.audit_path / "audit.jsonl"
        self.config = self._create_config()

        # 创建一些测试数据
        _write_audit_query_data(self.audit_path, self.audit_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_config(self):
        """创建测试配置。"""
        class MockConfig:
            audit_log_path = str(self.audit_path)
            audit_enabled = True
        return MockConfig()

    # LLM: 显式可信根是索引准入条件，原数量和记录断言保持不变。
    # 函数用途: 验证完整审计查询。
    def test_query_all(self):
        """测试查询所有。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(limit=10)

        self.assertEqual(result.total_count, 4)
        self.assertEqual(len(result.entries), 4)

    # LLM: owner根只界定来源，用户过滤仍由原精确条件决定。
    # 函数用途: 验证指定用户查询。
    def test_query_by_user(self):
        """测试按用户查询。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(user_id="alice", limit=10)

        self.assertEqual(result.total_count, 2)
        for entry in result.entries:
            self.assertEqual(entry.user_id, "alice")

    # LLM: 原动作条件及数量断言不因读投影改变。
    # 函数用途: 验证指定动作查询。
    def test_query_by_action(self):
        """测试按动作查询。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(action=AuditAction.CREATE_TASK, limit=10)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.entries[0].action, "CREATE_TASK")

    # LLM: 状态按原精确合同过滤，不从错误文本猜测。
    # 函数用途: 验证拒绝状态查询。
    def test_query_by_status(self):
        """测试按状态查询。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(status="denied", limit=10)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.entries[0].status, "denied")

    # LLM: 显式owner索引分页仍须保持总数和页间不重叠。
    # 函数用途: 验证原整数分页兼容。
    def test_query_pagination(self):
        """测试分页。"""
        query = AuditQuery(self.config, root=self.temp_dir)

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

    # LLM: 索引排序必须沿用原时间倒序，不能以物理行序替代。
    # 函数用途: 验证查询时间排序。
    def test_query_sorted_by_time(self):
        """测试按时间倒序。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(limit=10)

        # 应该按时间倒序
        timestamps = [e.timestamp for e in result.entries]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    # LLM: 完整覆盖下聚合仍须与原审计事实吻合。
    # 函数用途: 验证全部用户聚合摘要。
    def test_summary_all_users(self):
        """测试所有用户摘要。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        stats = query.summary()

        self.assertEqual(stats["total_actions"], 4)
        self.assertIn("CREATE_TASK", stats["by_action"])
        self.assertIn("DISPATCH", stats["by_action"])
        self.assertIn("success", stats["by_status"])
        self.assertIn("denied", stats["by_status"])

    # LLM: 聚合只统计精确用户条件，不扩大到同根其它身份。
    # 函数用途: 验证指定用户聚合摘要。
    def test_summary_specific_user(self):
        """测试指定用户摘要。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        stats = query.summary(user_id="alice")

        self.assertEqual(stats["total_actions"], 2)
        self.assertEqual(stats["user_id"], "alice")

    # LLM: 最近用户排序及身份仍来自实际审计时间。
    # 函数用途: 验证最近活跃用户。
    def test_recent_users(self):
        """测试最近活跃用户。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        users = query.recent_users(limit=10)

        self.assertEqual(len(users), 2)  # alice 和 bob

        # bob 应该是最新的（timestamp 最近）
        self.assertEqual(users[0]["user_id"], "bob")

    # LLM: 索引查询限制不改变原最新用户选择。
    # 函数用途: 验证活跃用户返回上限。
    def test_recent_users_limit(self):
        """测试限制用户数量。"""
        query = AuditQuery(self.config, root=self.temp_dir)
        users = query.recent_users(limit=1)

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["user_id"], "bob")  # 最新的

    # LLM: 显式清理仅影响过期条目，读投影须观察原文件新代次。
    # 函数用途: 验证清理后查询数量。
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

        query = AuditQuery(self.config, root=self.temp_dir)

        # 清理 1 天内的
        deleted = query.cleanup_old_entries(days=1)

        self.assertEqual(deleted, 1)

        # 再次查询
        result = query.query(limit=10)
        self.assertEqual(result.total_count, 4)  # 旧条目被删除

    # LLM: 零及负数不得隐式触发删除，读投影不能隐藏来源变化。
    # 函数用途: 验证永久保留口径。
    def test_cleanup_with_zero_or_negative_days_keeps_everything(self):
        """0 / 负数天表示永久保留：不删除任何记录。"""
        old_time = time.time() - 100000  # 超过一天
        old_entry = {
            "entry_id": "audit_old_keep",
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

        query = AuditQuery(self.config, root=self.temp_dir)
        before = query.query(limit=100).total_count

        self.assertEqual(query.cleanup_old_entries(days=0), 0)
        self.assertEqual(query.cleanup_old_entries(days=-5), 0)

        # 一条都没少（旧条目也仍在）
        self.assertEqual(query.query(limit=100).total_count, before)

    # LLM: 默认零保留策略不删除历史，原数量断言保持。
    # 函数用途: 验证未传天数不产生清理副作用。
    def test_cleanup_default_days_keeps_recent_entries(self):
        """不传天数时用默认保留期：一天前的条目不该被删。"""
        old_time = time.time() - 100000  # 超过一天
        old_entry = {
            "entry_id": "audit_old_default",
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

        query = AuditQuery(self.config, root=self.temp_dir)
        before = query.query(limit=100).total_count

        self.assertEqual(query.cleanup_old_entries(), 0)
        self.assertEqual(query.query(limit=100).total_count, before)

    # LLM: 实际空文件可以证明零；读取失败不能借该用例返回零。
    # 函数用途: 验证真实空来源查询。
    def test_empty_audit_file(self):
        """测试空审计文件。"""
        self.audit_file.write_text("")

        query = AuditQuery(self.config, root=self.temp_dir)
        result = query.query(limit=10)

        self.assertEqual(result.total_count, 0)
        self.assertEqual(len(result.entries), 0)


# LLM: 可信root必须显式传入，runtime路径优先且统计不得重扫原文件。
# 函数用途: 验证审计接线与拒绝猜测owner边界。
def test_indexed_audit_runtime_path_and_trusted_root(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.audit import query as module

    home = tmp_path / 'owner'
    home.mkdir()
    path = home / 'logs' / 'custom-audit.jsonl'
    _write_audit_query_data(path.parent, path)
    config = SimpleNamespace(audit_log_path='wrong-default')
    query = AuditQuery(config, root=home, runtime_audit_path=path)

    # LLM: 原始事实和SQL真实执行，仅拒绝旧全量物化入口。
    # 函数用途: 捕获未接入索引的读取。
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy audit scan')

    assert not hasattr(module, '_iter_audit_dicts')
    monkeypatch.setattr(module, '_iter_audit_dicts', forbidden, raising=False)
    result = query.query(limit=10)
    assert result.total_count == 4 and result.entries[0].entry_id == 'audit_4'
    assert query.summary('alice')['total_actions'] == 2
    assert query.recent_users(1)[0]['user_id'] == 'bob'
    assert not (home / 'wrong-default').exists()
    unbound = AuditQuery(config, runtime_audit_path=path)
    assert unbound.query_report()['coverage']['reason'] == 'trusted_root_required'
    with pytest.raises(ValueError) as caught:
        unbound.query()
    assert caught.value.coverage['reason'] == 'trusted_root_required'
    outside = AuditQuery(config, root=home / 'another', runtime_audit_path=path)
    assert outside.summary_report()['coverage']['reason'] == 'source_outside_trusted_root'


# LLM: 不可解码来源不是首次空账；读取不修复、覆盖或吞掉坏字节。
# 函数用途: 验证坏源返回未知总数和原字节保留。
def test_indexed_audit_corruption_is_not_empty(tmp_path):
    from types import SimpleNamespace

    import pytest

    path = tmp_path / 'audit.jsonl'
    path.write_bytes(b'\xff\n')
    query = AuditQuery(SimpleNamespace(audit_log_path=str(path)), root=tmp_path)
    assert callable(getattr(query, 'query_report', None))
    report = query.query_report()
    assert report['coverage']['status'] == 'unavailable' and report['total_count'] is None
    assert path.read_bytes() == b'\xff\n'
    with pytest.raises(ValueError) as caught:
        query.summary()
    assert caught.value.coverage['status'] == 'unavailable'


# LLM: 相同时间按物理顺序，固定view续页不能混入新追加。
# 函数用途: 验证审计稳定排序与固定总数。
def test_indexed_audit_equal_time_fixed_view(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / 'audit.jsonl'
    base = dict(timestamp=100, action='QUERY', user_id='alice', channel='chat',
                target_type='task', target_id='t', status='success', details={})
    path.write_text(''.join(json.dumps({**base, 'entry_id': name}) + '\n' for name in ('first', 'second')))
    query = AuditQuery(SimpleNamespace(audit_log_path=str(path)), root=tmp_path)
    assert callable(getattr(query, 'query_report', None))
    first = query.query_report(limit=1)
    assert first['entries'][0].entry_id == 'first'
    with path.open('a') as stream:
        stream.write(json.dumps({**base, 'entry_id': 'later'}) + '\n')
    second = query.query_report(limit=10, offset=1, view=first['view'])
    assert [entry.entry_id for entry in second['entries']] == ['second']
    assert second['total_count'] == 2 and query.query().total_count == 3


# LLM: 不依赖完整审计查询字段；清理只可依据有限数值时间戳，字节内容仍是原事实。
# 函数用途: 构造清理专用真实账本。
def _retention_case(tmp_path):
    target = tmp_path / 'security.jsonl'
    config = SimpleNamespace(audit_log_path=str(target), audit_enabled=True)
    return AuditQuery(config), target, config


# LLM: 缺省永久保留，旧cli字段不得重新触发隐式清理。
# 函数用途: 验证默认零及单次days不改配置。
def test_retention_default_zero(tmp_path):
    query, target, config = _retention_case(tmp_path)
    config.cli_audit_cleanup_days = 1
    config.security_audit_days = 0
    original = json.dumps({'timestamp': time.time() - 400 * 86400}).encode() + b'\n'
    target.write_bytes(original)
    assert query.cleanup_old_entries() == 0
    assert target.read_bytes() == original
    assert query.cleanup_old_entries(days=1) == 1
    assert config.security_audit_days == 0 and config.cli_audit_cleanup_days == 1


# LLM: 坏UTF8、未知/非有限时间戳及Unicode分隔符均不是删除或改写证据。
# 函数用途: 验证保留记录逐字节不变。
def test_retention_keeps_exact_unknown_bytes(tmp_path):
    query, target, _config = _retention_case(tmp_path)
    old = json.dumps({'timestamp': time.time() - 10 * 86400}).encode() + b'\n'
    fresh = b' \t' + json.dumps({'timestamp': time.time(), 'text': '界\u0085\u2028'},
                               ensure_ascii=False).encode() + b'\r\n'
    unknown = (b'\r\nnot-json\r\n{}\n[]\nnull\n{"timestamp":"0"}\n'
               b'{"timestamp":false}\n{"timestamp":NaN}\n{"timestamp":Infinity}\n'
               b'{"timestamp":-Infinity}\n{"timestamp":0,"text":"\xff"}\n\xffunfinished')
    target.write_bytes(old + fresh + unknown)
    assert query.cleanup_old_entries(days=1) == 1
    assert target.read_bytes() == fresh + unknown
    assert not list(tmp_path.glob('*.tmp'))


# LLM: 零删除不创建临时文件、不改变源代次。
# 函数用途: 检查清理空操作不产生写副作用。
def test_retention_noop_does_not_write(tmp_path):
    query, target, _config = _retention_case(tmp_path)
    original = json.dumps({'timestamp': time.time()}).encode() + b'\r\n'
    target.write_bytes(original)
    before = target.stat()
    assert query.cleanup_old_entries(days=1) == 0
    assert target.read_bytes() == original and target.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(tmp_path.glob('*.tmp'))


# LLM: 路径与FD两个真实读入口均注入同一个失败，不能将失败吞为零。
# 函数用途: 检查原始读取异常传播及原文件保留。
def test_retention_read_error_is_not_zero(tmp_path, monkeypatch):
    import io
    import os

    query, target, _config = _retention_case(tmp_path)
    original = json.dumps({'timestamp': 0}).encode() + b'\n'
    target.write_bytes(original)
    info = target.stat()
    failure = PermissionError('denied')
    opened = io.open

    # LLM: 只拦目标读取，不妨碍现有append旁锁。
    # 函数用途: 让旧read_text与新fdopen均收到真实权限失败。
    def failed(file, mode='r', *args, **kwargs):
        current = os.fstat(file) if isinstance(file, int) else None
        same = (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino) if current else Path(file) == target
        if same and 'r' in mode:
            raise failure
        return opened(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, 'open', failed)
    with pytest.raises(PermissionError) as caught:
        query.cleanup_old_entries(days=1)
    assert caught.value is failure
    monkeypatch.undo()
    assert target.read_bytes() == original


# LLM: 超预算必须显式拒绝且保持源事实，不为维护接口提供无界内存读取。
# 函数用途: 复现清理规划没有构建期字节硬界。
def test_retention_budget_failure_preserves_source(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit import query as module

    query, target, _config = _retention_case(tmp_path)
    original = b'{"timestamp":0}\n'
    target.write_bytes(original)
    monkeypatch.setattr(module, '_AUDIT_RETENTION_MAX_BYTES', 1, raising=False)
    with pytest.raises(OSError, match='budget'):
        query.cleanup_old_entries(days=1)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


# LLM: 在规划与提交间真实追加或同大小原子替换，旧generation必须失效。
# 函数用途: 检查清理不覆盖另一代或新增审计事实。
@pytest.mark.parametrize('change', ['append', 'replace'])
def test_retention_stale_plan_is_rejected(tmp_path, change):
    import os

    from agent_py_agent.agent.audit import query as module
    from agent_py_agent.agent.common.json_io import locked_json_path

    _query, target, config = _retention_case(tmp_path)
    target.write_bytes(b'{"timestamp":0}\n')
    with locked_json_path(target):
        plan = module._plan_audit_retention(target, cutoff_time=time.time())
    if change == 'append':
        AuditLogger(config).log(LogParams(action=AuditAction.QUERY, user_id='new', channel='chat',
                                         target_type='task', target_id='t'))
    else:
        previous = target.stat()
        replacement = tmp_path / 'replacement.jsonl'
        replacement.write_bytes(target.read_bytes())
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(target)
    changed = target.read_bytes()
    with locked_json_path(target), pytest.raises(BlockingIOError):
        module._apply_audit_retention_unlocked(plan)
    assert target.read_bytes() == changed


# LLM: 在真实plan边界启动真实logger，用nonblocking竞争证明共用原锁，随后阻塞追加必须保留。
# 函数用途: 防止清理读改写丢失并发审计追加。
def test_retention_serializes_with_logger(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.audit import query as module

    query, target, config = _retention_case(tmp_path)
    fresh = json.dumps({'timestamp': time.time()}).encode() + b'\n'
    target.write_bytes(b'{"timestamp":0}\n' + fresh)
    rejected = threading.Event()
    errors, appended = [], []
    original = module._plan_audit_retention

    # LLM: 使用原logger和锁，不模拟成功写入或锁状态。
    # 函数用途: 测试竞争拒绝后实际完成一次追加。
    def competing():
        try:
            logger = AuditLogger(config)
            try:
                logger.log(LogParams(action=AuditAction.QUERY, user_id='nb', channel='chat',
                                     target_type='task', target_id='t'), blocking=False)
            except BlockingIOError:
                rejected.set()
            else:
                errors.append('cleanup did not hold append lock')
            appended.append(logger.log(LogParams(action=AuditAction.QUERY, user_id='new', channel='chat',
                                                target_type='task', target_id='t')))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=competing, daemon=True)

    # LLM: 原计划已完成但cleanup锁尚未退出，此时必须观察真实追加竞争拒绝。
    # 函数用途: 稳定安排竞争时序，不靠sleep碰运气。
    def planning(*args, **kwargs):
        plan = original(*args, **kwargs)
        worker.start()
        assert rejected.wait(3)
        return plan

    monkeypatch.setattr(module, '_plan_audit_retention', planning)
    try:
        assert query.cleanup_old_entries(days=1) == 1
    finally:
        if worker.ident is not None:
            worker.join(3)
    assert not worker.is_alive() and errors == [] and appended
    assert target.read_bytes().startswith(fresh)
    assert json.loads(target.read_bytes().split(b'\n')[-2])['entry_id'] == appended[0].entry_id


if __name__ == "__main__":
    unittest.main()
