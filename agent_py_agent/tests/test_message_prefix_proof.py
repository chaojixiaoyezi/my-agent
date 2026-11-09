"""纯提示的否定证明：碰撞只能丢优化，覆盖、身份和容量不能猜。"""
import threading

from agent_py_agent.agent.conversation.message_prefix_proof import HotPrefixCache
from agent_py_agent.agent.io.cursor_cache import FileStamp, note_seen

STAMP = FileStamp(1, 2, 1000, 3, 4)


def _begin(cache, path='source', start=0, stamp=STAMP, changed=5, trusted=True):
    return cache.begin(path, stamp, changed, start, trusted=trusted)


def test_contiguous_prefix_can_prove_only_new_short_ids():
    cache = HotPrefixCache(filter_bytes=1024, max_sources=2, min_source_bytes=0)
    first = _begin(cache)
    assert first.observe(0, 10, 'old')
    first.close(verified=True)
    second = _begin(cache, start=10)
    assert not second.observe(10, 20, 'old')
    assert second.observe(20, 30, 'new')
    second.close(verified=True)
    assert cache.usage() == (1, 1024)
    assert not second.observe(30, 40, 'late')
    second.close(verified=True)


def test_real_filter_collision_is_only_a_miss_not_first_occurrence():
    bits = bytearray(1)
    assert not note_seen(bits, 'a')
    collision = next(str(i) for i in range(1000) if note_seen(bytearray(bits), str(i)))
    cache = HotPrefixCache(filter_bytes=1, min_source_bytes=0)
    one = _begin(cache)
    assert one.observe(0, 10, 'a')
    assert not one.observe(10, 20, collision)
    one.close(verified=True)


def test_changed_generation_same_stat_and_foreign_source_cannot_inherit_prefix():
    cache = HotPrefixCache(filter_bytes=16, min_source_bytes=0)
    one = _begin(cache)
    one.observe(0, 10, 'old')
    one.close(verified=True)
    assert _begin(cache, start=10, changed=6) is None
    assert _begin(cache, 'foreign', start=10) is None
    assert _begin(cache, start=0, trusted=False) is None
    fresh = _begin(cache, changed=6)
    assert fresh.observe(0, 10, 'old')
    fresh.close(verified=True)


def test_gaps_blank_or_failed_final_verification_cannot_publish_coverage():
    for invalid in ('gap', 'blank', 'failed'):
        cache = HotPrefixCache(filter_bytes=16, min_source_bytes=0)
        one = _begin(cache)
        assert one.observe(0, 10, 'old')
        if invalid == 'gap':
            assert not one.observe(11, 20, 'later')
        elif invalid == 'blank':
            assert not one.observe(10, 20, None)
        one.close(verified=invalid != 'failed')
        assert _begin(cache, start=10) is None


def test_active_entries_are_not_evicted_and_contention_does_not_wait():
    cache = HotPrefixCache(filter_bytes=16, max_sources=1, min_source_bytes=0)
    one = _begin(cache)
    observed = []
    thread = threading.Thread(target=lambda: observed.extend([
        _begin(cache), _begin(cache, 'foreign')]))
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and observed == [None, None]
    assert cache.usage() == (1, 16)
    one.close(verified=True)
    other = _begin(cache, 'foreign')
    assert one._entry is None
    assert cache.usage() == (1, 16)
    other.close()


def test_many_sources_keep_fixed_total_bitset_bytes():
    cache = HotPrefixCache(filter_bytes=64, max_sources=2, min_source_bytes=0)
    for number in range(100):
        one = _begin(cache, str(number))
        one.observe(0, 10, str(number))
        one.close(verified=True)
        assert cache.usage() == (min(number + 1, 2), 64 * min(number + 1, 2))


def test_foreign_thread_cannot_advance_or_close_borrowed_proof():
    cache = HotPrefixCache(filter_bytes=64, min_source_bytes=0)
    one = _begin(cache)
    result = []

    def foreign():
        result.append(one.observe(0, 10, 'foreign'))
        one.close(verified=True)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and result == [False]
    assert one.observe(0, 10, 'owner')
    one.close(verified=True)
    following = _begin(cache, start=10)
    assert following is not None
    following.close(verified=True)
