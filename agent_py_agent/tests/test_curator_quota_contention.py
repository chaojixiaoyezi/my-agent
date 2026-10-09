"""mc3-e11dquota（MC4-2501）：配额锁竞争不得被记成恢复失败；策略/存储故障仍必须失败。

覆盖任务书测试清单：
- 竞争（另一个真实子进程暂时持有 owner 配额锁）→ busy 或等锁后成功，绝不 recovery failed；
- 配额策略读不了 → 失败（不得被吞成 busy）；
- 存储错误（用量扫描失败）→ 失败且错误码不是模型失败。

真实配额锁、真实子进程；只用 pytest 临时目录，不碰真实 home。
"""

from __future__ import annotations

import select
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

from agent_py_agent.agent.user_space import owner_quota as oq
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.tests import test_memory_curator_v2 as fixtures

_QUOTA_BYTES = 1024 ** 3

# LLM: 子进程持锁等待 stdin，主进程可在真实文件锁竞争下观察 curator 的准入行为。
_HOLDER = """
import sys
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
with OwnerQuotaEnforcer(sys.argv[1], max_bytes=1024 ** 3).admission():
    print('locked', flush=True)
    sys.stdin.readline()
"""


def _service_at(root: Path):
    store, thread, message = fixtures._conversation(root)
    output = fixtures._valid_output(thread.thread_id, message.message_id, message.content)
    return fixtures._service(root, fixtures._StaticStructuredBackend(output), store)


def _attach_quota(service, root: Path) -> OwnerQuotaEnforcer:
    quota = OwnerQuotaEnforcer(root, max_bytes=_QUOTA_BYTES)
    service.committer.candidates.quota_enforcer = quota
    service.committer.daily.quota_enforcer = quota
    return quota


# LLM: 子进程持锁时发起一次 run；2 秒内返回即记录，否则释放锁后等它完成。
def _decide_while_locked(pool, service, child):
    future = pool.submit(service.run, reason='admin')
    try:
        result = future.result(timeout=2)
    except TimeoutError:
        result = None
    child.stdin.write('release\n')
    child.stdin.flush()
    out, err = child.communicate(timeout=10)
    assert child.returncode == 0, (out, err)
    return result if result is not None else future.result(timeout=20)


# LLM: 修复前该场景返回 failed/CURATOR_COMMIT_RECOVERY_FAILED（锁竞争被吞进恢复失败）；
#   现在必须 busy（立即返回）或等锁释放后 succeeded（MC4-2501）。
def test_quota_lock_contention_is_busy_or_waits_then_succeeds(tmp_path):
    service = _service_at(tmp_path)
    _attach_quota(service, tmp_path)
    child = subprocess.Popen(
        [sys.executable, '-c', _HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline().strip() == 'locked'
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = _decide_while_locked(pool, service, child)
        assert result.status in {'busy', 'succeeded'}, result
        assert result.failure_code != 'CURATOR_COMMIT_RECOVERY_FAILED'
    finally:
        if child.poll() is None:
            child.communicate('release\n', timeout=10)


# LLM: 策略不可用是持久故障，不是暂态竞争；必须报失败而不是 busy。
def test_quota_policy_unavailable_is_failure_not_busy(tmp_path):
    service = _service_at(tmp_path)
    quota = OwnerQuotaEnforcer(tmp_path, max_bytes=_QUOTA_BYTES, policy_available=False)
    service.committer.candidates.quota_enforcer = quota
    service.committer.daily.quota_enforcer = quota
    result = service.run(reason='admin')
    assert result.status == 'failed', result
    assert result.failure_code != 'CURATOR_MODEL_FAILED'


# LLM: 存储错误（用量扫描 OSError）同样必须失败，且不得被归成模型失败或 busy。
def test_quota_scan_storage_error_is_failure_not_model_failure(tmp_path, monkeypatch):
    service = _service_at(tmp_path)
    _attach_quota(service, tmp_path)

    def fail(root):
        raise OSError('synthetic owner usage scan failure')

    monkeypatch.setattr(oq, '_owner_usage_bytes_for_admission', fail)
    result = service.run(reason='admin')
    assert result.status == 'failed', result
    assert result.failure_code != 'CURATOR_MODEL_FAILED'
