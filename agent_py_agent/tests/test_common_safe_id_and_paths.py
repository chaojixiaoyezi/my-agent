"""批1 冗余收敛钉子:safe_id 与 normalize_path 唯一权威(体检实锤:safe_id
4 处实现三种默认值两种字符集;路径规范 7 种模式多数缺 expandvars)。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.common.path_normalize import normalize_path
from agent_py_agent.agent.common.safe_id import safe_id

pytestmark = pytest.mark.integration


def test_safe_id_semantics_cover_all_legacy_call_sites() -> None:
    assert safe_id("任务/名 字!") == "-".join([]) or safe_id("a/b c") == "a-b-c"
    assert safe_id("", default="main") == "main"
    assert safe_id("", default="run") == "run"
    assert safe_id("", default="unknown") == "unknown"
    assert safe_id("x" * 100, max_len=48) == "x" * 48
    assert safe_id("v1.2.3") == "v1.2.3", "保留点号(task_progress 既有语义)"
    assert safe_id("--a--") == "a", "首尾 -_ 剥离"


def test_normalize_path_expands_vars_user_and_resolves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_TEST_ROOT", str(tmp_path))
    result = normalize_path("$MY_TEST_ROOT/sub/../sub/file.txt")
    assert result == tmp_path / "sub" / "file.txt", "expandvars+resolve 全链生效"
    assert normalize_path("") is None and normalize_path(None) is None
    home = normalize_path("~/x")
    assert home is not None and str(home).startswith(str(Path.home()))


def test_read_text_lines_cached_serves_unchanged_and_invalidates(tmp_path):
    """批4 性能小修钉子:同签名命中缓存(同一 tuple 对象);文件变化即失效。"""
    from agent_py_agent.agent.common.json_io import read_text_lines_cached

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    first = read_text_lines_cached(ledger)
    second = read_text_lines_cached(ledger)
    assert first == ('{"a":1}', '{"b":2}')
    assert second is first, "签名未变必须命中缓存(同一不可变 tuple)"

    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"c":3}\n')
    third = read_text_lines_cached(ledger)
    assert third == ('{"a":1}', '{"b":2}', '{"c":3}'), "append 后 size 变化即重读"

    import pytest

    with pytest.raises(FileNotFoundError):
        read_text_lines_cached(tmp_path / "missing.jsonl")


def test_model_endpoint_env_exported_for_subprocesses(tmp_path, monkeypatch):
    """R13c 实锤钉子:agent 初始化必须把自用端点暴露为 AGENT_API_BASE/
    AGENT_MODEL_NAME(子进程 LLM 子调用不再猜端点);用户已设值不覆盖。"""
    import os

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.delenv("AGENT_API_BASE", raising=False)
    monkeypatch.setenv("AGENT_MODEL_NAME", "user-pinned-model")
    SimpleAgent(
        AgentConfig(my_agent_home=str(tmp_path / "h"), prompt_files=[], enable_tools=False,
                    api_base="https://example.test/anthropic", model_name="test-model"),
        tmp_path,
    )
    assert os.environ["AGENT_API_BASE"] == "https://example.test/anthropic"
    assert os.environ["AGENT_MODEL_NAME"] == "user-pinned-model", "setdefault 语义:用户显式值优先"
