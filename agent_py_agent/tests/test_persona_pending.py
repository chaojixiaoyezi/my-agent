"""待确认人设写入存储(飞书卡片确认)单测:add/load/pop 往返、pop 原子领取(重复只一次)、
TTL 过期作废、purge 清过期、非法 token 拒绝。pop 的"只有一个调用能拿到"是"卡片重复回调不重复写"
的幂等基石,必须锁死。"""

from __future__ import annotations

import time

from agent_py_agent.agent.capability import persona_pending


def _add(root, target="soul", content="语气偏活泼", owner_id="ou_abc"):
    return persona_pending.add(root, ("feishu", "user", owner_id), target, content)


def test_add_load_roundtrip(tmp_path):
    token = _add(tmp_path, content="以后叫我小王")
    rec = persona_pending.load(tmp_path, token)
    assert rec is not None
    assert rec.token == token
    assert rec.owner_provider == "feishu" and rec.owner_kind == "user" and rec.owner_id == "ou_abc"
    assert rec.target == "soul" and rec.content == "以后叫我小王"
    # 文件落在固定路径 <root>/pending_persona/<token>.json(网关/适配器同根都能读)
    assert (tmp_path / "pending_persona" / f"{token}.json").is_file()


def test_load_does_not_delete(tmp_path):
    token = _add(tmp_path)
    assert persona_pending.load(tmp_path, token) is not None
    assert persona_pending.load(tmp_path, token) is not None  # load 只读不删


def test_pop_returns_record_then_gone(tmp_path):
    token = _add(tmp_path, content="产物用 HTML")
    rec = persona_pending.pop(tmp_path, token)
    assert rec is not None and rec.content == "产物用 HTML"
    assert persona_pending.load(tmp_path, token) is None  # pop 后记录已删


def test_pop_is_atomic_single_winner(tmp_path):
    # 关键幂等保证:同一 token 连 pop 两次,只有第一次拿到记录,第二次 None(卡片重复回调不重复写)
    token = _add(tmp_path)
    first = persona_pending.pop(tmp_path, token)
    second = persona_pending.pop(tmp_path, token)
    assert first is not None
    assert second is None


def test_pop_concurrent_only_one_wins(tmp_path):
    import threading

    token = _add(tmp_path)
    results: list = []
    barrier = threading.Barrier(8)

    def _worker():
        barrier.wait()
        results.append(persona_pending.pop(tmp_path, token))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1  # 并发下也只有一个领取成功


def test_expired_record_not_returned(tmp_path):
    token = _add(tmp_path)
    # ttl=0 → 立刻过期:load/pop 都视为失效
    assert persona_pending.load(tmp_path, token, ttl_seconds=0) is None
    assert persona_pending.pop(tmp_path, token, ttl_seconds=0) is None


def test_purge_expired_removes_stale(tmp_path):
    fresh = _add(tmp_path, content="新的")
    stale = _add(tmp_path, content="旧的")
    # 把 stale 的 created_at 改老
    stale_path = tmp_path / "pending_persona" / f"{stale}.json"
    import json

    data = json.loads(stale_path.read_text(encoding="utf-8"))
    data["created_at"] = time.time() - 10 * 3600
    stale_path.write_text(json.dumps(data), encoding="utf-8")
    removed = persona_pending.purge_expired(tmp_path, ttl_seconds=3600)
    assert removed == 1
    assert persona_pending.load(tmp_path, fresh) is not None  # 新的保留
    assert not stale_path.exists()  # 旧的清掉


def test_purge_removes_corrupt_file(tmp_path):
    _add(tmp_path)
    bad = tmp_path / "pending_persona" / "deadbeef.json"
    bad.write_text("{not json", encoding="utf-8")
    removed = persona_pending.purge_expired(tmp_path, ttl_seconds=99999)
    assert removed == 1 and not bad.exists()  # 损坏文件也清


def test_invalid_or_missing_token(tmp_path):
    assert persona_pending.load(tmp_path, "") is None
    assert persona_pending.pop(tmp_path, "../etc/passwd") is None  # 路径穿越 token 拒绝
    assert persona_pending.load(tmp_path, "no_such_token_xxxx") is None


def test_purge_on_missing_dir_is_zero(tmp_path):
    assert persona_pending.purge_expired(tmp_path / "nope") == 0
