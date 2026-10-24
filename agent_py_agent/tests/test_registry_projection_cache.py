# LLM: 真实no-follow登记表读取的缓存反例；不以stat或旧观测代替当前内容。
# 模块用途: 验证同raw共享解析、独立副本、损坏和变化不能误命中。
import json
import os

import pytest

from agent_py_agent.agent.artifacts import registry


# LLM: 用普通单链接账本，正文仍经产品的安全读取入口。
# 函数用途: 创建指定raw的测试登记表。
def _ledger(root, raw):
    path = registry.registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return path


# LLM: spy统计真实json.loads，文件读取次数独立统计，不能因缓存跳过第二次安全读。
# 函数用途: 验证两个业务查询共享一次解析且副本不会污染缓存。
def test_exact_raw_parses_once_but_reads_every_time(tmp_path, monkeypatch):
    raw = json.dumps({"artifact_id": "a", "metadata": {
        "protection_operation_id": "op", "backup_ref": "ref", "items": [{"x": 1}]}}) + "\n"
    _ledger(tmp_path, raw)
    loads, reads = [], []
    original_loads, original_read = json.loads, registry.read_text_beneath

    # LLM: 转调真实JSON解析，不改payload或异常。
    # 函数用途: 数出实际解析次数。
    def parse(text, *args, **kwargs):
        loads.append(text)
        return original_loads(text, *args, **kwargs)

    # LLM: 每次依旧走no-follow读取；仅记读取次数。
    # 函数用途: 验证缓存命中没有跳过内容证据。
    def read(*args):
        reads.append(args)
        return original_read(*args)

    monkeypatch.setattr(json, "loads", parse)
    monkeypatch.setattr(registry, "read_text_beneath", read)
    first = registry.latest_artifact_records_report(tmp_path)
    first.records["a"].metadata["items"][0]["x"] = 999
    second = registry.latest_artifact_records_report(tmp_path)
    ids = registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "ref"})
    assert len(loads) == 1
    assert len(reads) == 3
    assert second.records["a"].metadata["items"] == [{"x": 1}]
    assert ids == {"a"}


# LLM: 同大小覆盖并恢复mtime以及原子替换都用真实文件，不伪造stat cache hit。
# 函数用途: 验证精确raw变化即刷新；不同root不能共享带路径的错误或记录对象。
def test_changed_bytes_same_stat_and_atomic_replace(tmp_path):
    root = tmp_path / "one"
    path = _ledger(root, '{"artifact_id":"a","path":"one"}\n')
    assert registry.latest_artifact_records(root)["a"].path == "one"
    before = path.stat()
    path.write_text('{"artifact_id":"a","path":"two"}\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert registry.latest_artifact_records(root)["a"].path == "two"
    replacement = path.with_suffix(".new")
    replacement.write_text('{"artifact_id":"a","path":"new"}\n')
    replacement.replace(path)
    assert registry.latest_artifact_records(root)["a"].path == "new"
    _ledger(tmp_path / "other", '{"artifact_id":"a","path":"new"}\n')
    assert registry.latest_artifact_records(tmp_path / "other")["a"].path == "new"


# LLM: 报告保留坏行诊断，恢复扫描必须继续拒绝损坏，不把缓存错误转为空成功。
# 函数用途: 验证坏JSON和非法记录的两种原业务错误口径。
@pytest.mark.parametrize("bad", ["{bad", "[]", '{"metadata":1}'])
def test_corruption_remains_visible_across_hits(tmp_path, bad):
    _ledger(tmp_path, '{"artifact_id":"good"}\n' + bad + "\n")
    first = registry.latest_artifact_records_report(tmp_path)
    second = registry.latest_artifact_records_report(tmp_path)
    assert first.errors == second.errors and len(second.errors) == 1
    assert second.errors[0]["line"] == 2
    assert "good" in second.records
    if bad != '{"metadata":1}':
        with pytest.raises(OSError):
            registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "r"})


# LLM: 缓存命中前仍须读取，拒绝/编码错误不能被旧成功吞掉。
# 函数用途: 验证已有缓存不掩盖当次读取拒绝。
def test_cache_never_masks_current_read_failure(tmp_path, monkeypatch):
    _ledger(tmp_path, '{"artifact_id":"a"}\n')
    assert "a" in registry.latest_artifact_records(tmp_path)

    # LLM: 仅注入本次reader权限拒绝，不改缓存或伪造空文件。
    # 函数用途: 模拟安全读在命中之后失败。
    def denied(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(registry, "read_text_beneath", denied)
    report = registry.latest_artifact_records_report(tmp_path)
    assert not report.records and report.errors[0]["error_type"] == "PermissionError"
    with pytest.raises(OSError, match="cannot be scanned safely"):
        registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "r"})


# LLM: 同词法root被替换成新目录且raw相等，仍按当前安全目录身份分区。
# 函数用途: 验证根目录换代不能沿用旧身份缓存。
def test_root_replacement_is_a_new_cache_partition(tmp_path, monkeypatch):
    from agent_py_agent.agent.artifacts import registry_projection_cache as cache

    root, raw = tmp_path / "root", '{"artifact_id":"a"}\n'
    _ledger(root, raw)
    calls, original = [], cache.parse_registry_projection

    # LLM: 锁外真实解析，不更改行为。
    # 函数用途: 观察根换代是否重新解析。
    def parse(text):
        assert not cache._LOCK.locked()
        calls.append(text)
        return original(text)

    monkeypatch.setattr(cache, "parse_registry_projection", parse)
    assert "a" in registry.latest_artifact_records(root)
    root.rename(tmp_path / "old")
    _ledger(root, raw)
    assert "a" in registry.latest_artifact_records(root)
    assert len(calls) == 2


# LLM: 独立测试缓存实例以免污染其它用例；预算用实际深层体积推导，不依赖CPython对象固定大小。
# 函数用途: 验证LRU总预算、每root最近一版和超大raw不截断不缓存。
def test_lru_budget_and_oversized_raw_are_lossless(monkeypatch):
    from collections import OrderedDict

    from agent_py_agent.agent.artifacts import registry_projection_cache as cache

    monkeypatch.setattr(cache, "_CACHE", OrderedDict())
    monkeypatch.setattr(cache, "_CACHE_BYTES", 0)
    raw = '{"artifact_id":"a"}\n'
    projection = cache.parse_registry_projection(raw)
    cost = cache._memory_cost(((("r1", 1, 1), cache.PARSER_VERSION), projection))
    monkeypatch.setattr(cache, "CACHE_MAX_BYTES", cost + 100)
    first = cache.registry_projection(("r1", 1, 1), raw)
    cache.registry_projection(("r2", 1, 2), raw)
    assert len(cache._CACHE) == 1 and cache._CACHE_BYTES <= cache.CACHE_MAX_BYTES
    assert cache.registry_projection(("r1", 1, 1), raw) is not first
    large = '{"artifact_id":"a","metadata":{"body":"' + 'x' * cost + '"}}\n'
    result = cache.registry_projection(("r1", 1, 1), large)
    assert result.raw == large
    assert result.records[0].payload["metadata"]["body"] == 'x' * cost
    assert not cache._CACHE
    assert cache._CACHE_BYTES == 0


# LLM: 投影不可变到metadata叶子；失败诊断与旧解析器逐字段对齐，不只检查存在。
# 函数用途: 核对JSON损坏、非法record和深层副本的完整旧错误合同。
def test_projection_errors_match_original_parser_and_are_immutable(tmp_path):
    from agent_py_agent.agent.artifacts import registry_projection_cache as cache
    from agent_py_agent.agent.artifacts.registry_reader import latest_records_from_lines

    raw = '{"artifact_id":"a","metadata":{"items":[{"x":1}]}}\n{bad\n[]\n{"size_bytes":"bad"}\n'
    path = _ledger(tmp_path, raw)
    projection = cache.registry_projection(cache.trusted_root_identity(tmp_path), raw)
    assert projection.parse_error
    with pytest.raises(TypeError):
        projection.records[0].payload["metadata"]["items"][0]["x"] = 999
    expected, errors = latest_records_from_lines(path, raw.splitlines(), registry._record_from_payload)
    report = registry.latest_artifact_records_report(tmp_path)
    assert report.records == expected and report.errors == errors
    report.errors[0]["line"] = 999
    assert registry.latest_artifact_records_report(tmp_path).errors == errors


# LLM: 对照原JSON容器转换；列表键不能因冻结成tuple而意外变成可哈希的合法metadata。
# 函数用途: 验证同ID坏行不覆盖旧有效记录，非标量文本字段保持原str语义。
def test_freezing_never_changes_original_field_coercion(tmp_path):
    from agent_py_agent.agent.artifacts.registry_reader import latest_records_from_lines

    raw = ('{"artifact_id":"a","status":"ready"}\n'
           '{"artifact_id":"a","metadata":[[[1],"x"]]}\n'
           '{"artifact_id":["weird"],"path":["p"],"status":{"x":1}}\n')
    path = _ledger(tmp_path, raw)
    expected, errors = latest_records_from_lines(path, raw.splitlines(), registry._record_from_payload)
    assert len(errors) == 1 and expected["a"].status == "ready"
    for _ in range(2):
        report = registry.latest_artifact_records_report(tmp_path)
        assert report.records == expected and report.errors == errors


# LLM: 恢复扫描原本不转换updated_at；字段转换失败仅影响latest，不能因共享投影污染另一查询口径。
# 函数用途: 验证有效JSON的大整数浮点溢出不破坏operation历史查证。
def test_record_conversion_failure_does_not_change_operation_scan(tmp_path):
    raw = json.dumps({"artifact_id": "a", "updated_at": 10 ** 400,
                      "metadata": {"protection_operation_id": "op", "backup_ref": "r"}}) + "\n"
    _ledger(tmp_path, raw)
    assert registry.artifact_operation_recorded_ids(tmp_path, operation_key="op", expected_backup_refs={"a": "r"}) == {"a"}
    with pytest.raises(OverflowError):
        registry.latest_artifact_records_report(tmp_path)
