# LLM: 原拒绝日志只运输安全诊断，不授恢复/重试权；合成组件测试不读真实home或启动服务。
# 模块用途: 覆盖来源日志有界链、JSON数值位置、隐私和日志失败不覆盖原拒绝。
import errno
import json
import logging
import re
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.strict_json import load_strict_json
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as runtime


def _extra_data_error():
    try:
        load_strict_json('{"敏感正文":"不可输出"}\n{"第二根":"不可输出"}')
    except json.JSONDecodeError as error:
        return error
    raise AssertionError('expected real JSONDecodeError')


def _message(caplog):
    records = [record for record in caplog.records if record.name == runtime.__name__]
    assert len(records) == 1
    assert records[0].exc_info is None and records[0].stack_info is None
    return records[0].getMessage()


def test_json_decode_logs_product_caller_and_character_positions_without_body(caplog):
    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    error = _extra_data_error()
    original = (error.args, error.doc, error.msg, error.pos, error.lineno, error.colno, error.__traceback__)
    runtime._log_source_refusal('source_refresh_failed', error)
    text = _message(caplog)
    assert '_extra_data_error:' in text and 'load_strict_json:' in text
    assert f'json_pos={error.pos}' in text
    assert f'json_lineno={error.lineno}' in text and f'json_colno={error.colno}' in text
    assert f'json_doc_chars={len(error.doc)}' in text
    assert len(error.doc.encode()) != len(error.doc)
    for secret in ('敏感正文', '不可输出', '第二根', error.msg, str(error.args), __file__):
        assert secret not in text
    assert original == (error.args, error.doc, error.msg, error.pos, error.lineno, error.colno, error.__traceback__)


def test_origin_is_bounded_to_twelve_functions(caplog):
    caplog.set_level(logging.WARNING, logger=runtime.__name__)

    def descend(depth):
        if depth:
            return descend(depth - 1)
        raise ValueError('SECRET_MESSAGE')

    try:
        descend(24)
    except ValueError as error:
        runtime._log_source_refusal('source_refresh_failed', error)
    text = _message(caplog)
    origin = re.search(r'origin=(\S+)', text)[1]
    assert len(origin.split('/')) == 12
    assert all(re.fullmatch(r'descend:[0-9]+', frame) for frame in origin.split('/'))
    assert 'SECRET_MESSAGE' not in text


@pytest.mark.parametrize('error', [None, ValueError('SECRET_MESSAGE'), OSError(errno.EACCES, 'SECRET_MESSAGE', '/SECRET_PATH')])
def test_non_json_error_has_only_not_applicable_json_fields(caplog, error):
    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    runtime._log_source_refusal('source_guard_refused', error)
    text = _message(caplog)
    for name in ('json_pos', 'json_lineno', 'json_colno', 'json_doc_chars'):
        assert f'{name}=None' in text
    assert 'SECRET' not in text
    assert f'errno={errno.EACCES if isinstance(error, OSError) else None}' in text


def test_json_metadata_rejects_non_numeric_values_instead_of_printing_them(caplog):
    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    error = _extra_data_error()
    error.pos, error.lineno, error.colno, error.doc = 'SECRET_POS', object(), True, ['SECRET_DOC']
    runtime._log_source_refusal('source_refresh_failed', error)
    text = _message(caplog)
    assert 'SECRET' not in text and 'object at' not in text
    for name in ('json_pos', 'json_lineno', 'json_colno', 'json_doc_chars'):
        assert f'{name}=None' in text


@pytest.mark.parametrize('logger_fault', ['lookup', 'warning'])
def test_logging_fault_does_not_replace_original_error(monkeypatch, logger_fault):
    original = _extra_data_error()

    def fail(*_args, **_kwargs):
        raise RuntimeError('SECRET_LOGGER_FAILURE')

    if logger_fault == 'lookup':
        monkeypatch.setattr(runtime, 'logging', SimpleNamespace(getLogger=fail))
    else:
        monkeypatch.setattr(runtime, 'logging', SimpleNamespace(getLogger=lambda _name: SimpleNamespace(warning=fail)))
    try:
        try:
            raise original
        except json.JSONDecodeError as error:
            runtime._log_source_refusal('source_refresh_failed', error)
            raise
    except json.JSONDecodeError as observed:
        assert observed is original


@pytest.mark.parametrize('logger_fault', [False, True])
# LLM: 替身索引提供临时root和空metadata原stamp供真实来源预检，只在原角色门注入解析异常，不绕过新增固定来源检查。
# 函数用途: 验解析异常真实到达拒绝入口，仍隔离、返回False并记录原调用链。
def test_refresh_keeps_original_quarantine_and_false_result(monkeypatch, caplog, logger_fault, tmp_path):
    from agent_py_agent.agent.gateway_parts import (
        multiprocess_executor_active_index as active_index,
    )

    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    original = _extra_data_error()
    controller = SimpleNamespace(active=lambda: True, _all_quarantined=False, paths=object(),
                                 c_run_id='synthetic-current', count=2, _closed_role_sources=(),
                                 _bootstrap_sources=None, _standalone_migration=None, _standalone_origin=None)
    observed = []

    def refuse(*_args, **_kwargs):
        observed.append(original)
        raise original

    # LLM: 只为诊断测试补齐索引的读取合同；原角色拒绝后仍禁止继续检查active来源。
    # 函数用途: 让真实固定来源预检读取临时空根，而不是因替身缺字段提前失败。
    def locate(actual):
        assert actual is controller
        return SimpleNamespace(ready=True, root=tmp_path, _metadata_stamps={}, check_roles=refuse,
                               check_sources=lambda: pytest.fail('refused roles cannot check sources'))

    monkeypatch.setattr(active_index, 'index_for', locate)
    if logger_fault:
        def fail(*_args, **_kwargs):
            raise RuntimeError('SECRET_LOGGER_FAILURE')
        monkeypatch.setattr(runtime, 'logging', SimpleNamespace(getLogger=lambda _name: SimpleNamespace(warning=fail)))
    assert runtime._refresh_targets(controller) is False
    assert observed == [original]
    assert controller._all_quarantined is True
    assert controller._closed_role_sources == () and controller._bootstrap_sources is None
    assert original.doc == '{"敏感正文":"不可输出"}\n{"第二根":"不可输出"}'
    if not logger_fault:
        text = _message(caplog)
        assert '_refresh_targets:' in text and 'refuse:' in text
        assert '不可输出' not in text


@pytest.mark.parametrize('value', [-1, 10**100, True])
def test_invalid_numeric_positions_are_not_logged(caplog, value):
    caplog.set_level(logging.WARNING, logger=runtime.__name__)
    error = _extra_data_error()
    error.pos = error.lineno = error.colno = value
    runtime._log_source_refusal('source_refresh_failed', error)
    text = _message(caplog)
    for name in ('json_pos', 'json_lineno', 'json_colno'):
        assert f'{name}=None' in text
    assert f'json_doc_chars={len(error.doc)}' in text


# LLM: 替身提供真实临时root和空metadata原stamp，确保坏异常属性进入原角色拒绝及日志保护，不被来源预检的字段错误掩盖。
# 函数用途: 验日志读取异常属性失败时，原刷新仍保持False和整体隔离。
def test_exception_attribute_failure_keeps_original_refusal(monkeypatch, tmp_path):
    from agent_py_agent.agent.gateway_parts import (
        multiprocess_executor_active_index as active_index,
    )

    class BrokenJSONError(json.JSONDecodeError):
        def __getattribute__(self, name):
            if name == 'pos':
                raise ValueError('SECRET_ATTRIBUTE_FAILURE')
            return super().__getattribute__(name)

    original = BrokenJSONError('SECRET_MESSAGE', '{"SECRET_DOC":1}', 0)
    controller = SimpleNamespace(active=lambda: True, _all_quarantined=False, paths=object(),
                                 c_run_id='synthetic-current', count=2, _closed_role_sources=(),
                                 _bootstrap_sources=None, _standalone_migration=None, _standalone_origin=None)
    observed = []

    def refuse(*_args, **_kwargs):
        observed.append(original)
        raise original

    # LLM: 只补诊断替身的新增来源字段，真实固定来源reader与原异常处理均不替换。
    # 函数用途: 允许预检临时根后到达原定坏异常属性观察口。
    def locate(actual):
        assert actual is controller
        return SimpleNamespace(ready=True, root=tmp_path, _metadata_stamps={}, check_roles=refuse,
                               check_sources=lambda: pytest.fail('refused roles cannot check sources'))

    monkeypatch.setattr(active_index, 'index_for', locate)
    assert runtime._refresh_targets(controller) is False
    assert observed == [original]
    assert controller._all_quarantined is True
