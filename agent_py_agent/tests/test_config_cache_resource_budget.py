# LLM: 只在临时工作区检验原子代理持久化与缓存，不访问真实owner，不以缓存替代canonical。
# 模块用途: 覆盖未知字段保真、canonical失效与缓存锁外读取的资源预算。
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: 修改临时canonical以模拟新代际字段；必须沿原manager保存，不通过替身绕开最终写门。
# 函数用途: 验证本轮未认识的字段不会在读取和再次保存后丢失。
def test_subagent_unknown_fields_survive_full_save(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="保留未来字段", thought="检查读写合同", plan=["保存"])
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    raw["future_recovery"] = {"unknown_count": 7, "pins": [{"id": "receipt-1"}]}
    path.write_text(json.dumps(raw))
    loaded = manager.load(task.id)
    assert getattr(loaded, "_extra_fields", {}).get("future_recovery") == raw["future_recovery"]
    manager.save(loaded)
    assert json.loads(path.read_text())["future_recovery"] == raw["future_recovery"]


# LLM: 原缓存必须释放锁才读取/解析文件，避免一个慢磁盘阻塞其它run；不伪造缓存命中。
# 函数用途: 用原load接缝观察解析是否仍在缓存锁内。
def test_subagent_cache_load_occurs_outside_cache_lock(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="慢读取不持锁", thought="检查缓存并发", plan=["读取"])
    service = manager.persistence
    original = service.load
    observed = []

    # LLM: 仅观察当前线程持锁事实，再调用真实load；不改变返回值或磁盘记录。
    # 函数用途: 捕捉缓存热路径的锁范围。
    def load(run_id):
        observed.append(service._run_cache_lock._is_owned())
        return original(run_id)

    monkeypatch.setattr(service, "load", load)
    service._cached_run_copy(task.id, tmp_path / task.id / "task.json")
    assert observed == [False]


# LLM: locator不变而canonical变化仍需失效；测试不靠睡眠或回拨时钟制造新鲜度。
# 函数用途: 防止缓存把直接修复的canonical误当旧任务返回。
def test_subagent_cache_rechecks_canonical_fingerprint(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service as module

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="独立canonical变更", thought="检查实时读取", plan=["读取"])
    service = manager.persistence
    monkeypatch.setattr(module, "_now", lambda: 10**12)
    locator = tmp_path / task.id / "task.json"
    first = service._cached_run_copy(task.id, locator)
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    raw["current_step"] = "fresh-canonical"
    path.write_text(json.dumps(raw))
    second = service._cached_run_copy(task.id, locator)
    assert first.current_step != second.current_step
    assert second.current_step == "fresh-canonical"


# LLM: 固定指纹模拟粗时间戳碰撞，locator已老而canonical刚改时，命中和发布都必须拒绝缓存。
# 函数用途: 验证双文件的信任年龄，而不是只依赖locator年龄。
def test_subagent_cache_requires_canonical_trust_age(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.subagents.services.persistence import service as module

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="双mtime", thought="保护canonical", plan=["读取"])
    service = manager.persistence
    locator = tmp_path / task.id / "task.json"
    canonical = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    now = module._now()
    monkeypatch.setattr(module, "_now", lambda: now)
    monkeypatch.setattr(module, "cache_stat_signature", lambda _stat: (1, 1, 1, 1, 1))
    os.utime(locator, (now - 60, now - 60))
    os.utime(canonical, (now - 60, now - 60))
    service._cached_run_copy(task.id, locator)
    assert task.id in service._run_cache
    raw = json.loads(canonical.read_text())
    raw["current_step"] = "young-canonical"
    canonical.write_text(json.dumps(raw))
    os.utime(canonical, (now, now))
    loaded = service._cached_run_copy(task.id, locator)
    assert loaded.current_step == "young-canonical"
    assert task.id not in service._run_cache
