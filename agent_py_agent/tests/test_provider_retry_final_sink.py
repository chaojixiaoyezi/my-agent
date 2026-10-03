"""修法 B 收口提示走结构化通道：final 事件、回退与 TUI/IM 文案一致（rfs）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as resume
from agent_py_agent.agent.backends import ProviderTimeoutError
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
    read_background_transcript_events,
)
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.gateway_parts.response_renderer import project_gateway_stream_chunk
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

FINAL_CODE = resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
# 已登记的用户文案：TUI（Gateway 流）与 IM（后台正文）必须显示同一句，不能各写一套。
FINAL_TEXT = gateway_client_error_message(FINAL_CODE)
# 触发重试的原始异常文本：回退文本逐字断言时一字不差地复现。
ORIGINAL_ERROR_TEXT = "模型接口等待首个流式事件超时"


# LLM: 假时钟只替换恢复器自己读的 time.monotonic；配合被替换的 wait_interruptibly
#   就能在毫秒内跑到「第二次等待越界」的收口分支，不真睡。
class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _prepare(monkeypatch, clock: _FakeClock) -> None:
    monkeypatch.setattr(resume, "time", type("T", (), {"monotonic": staticmethod(clock)})())
    monkeypatch.setattr(resume, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(resume, "provider_transient_retry_delays", lambda _policy=None: (10.0, 25.0))
    monkeypatch.setattr(resume, "provider_transient_total_budget_seconds", lambda _policy=None: 30.0)

    def wait(delay: float) -> None:
        clock.advance(delay)

    monkeypatch.setattr(resume, "wait_interruptibly", wait)


def _run_to_budget(monkeypatch, on_chunk) -> None:
    """跑一轮：第一次退避在预算内（普通重试），第二次等待越界（收口后抛出）。"""
    clock = _FakeClock()
    _prepare(monkeypatch, clock)
    original = ProviderTimeoutError(ORIGINAL_ERROR_TEXT, stage="first_event")

    def operation() -> str:
        raise original

    with pytest.raises(ProviderTimeoutError):
        resume.run_with_provider_transient_auto_resume(operation, on_chunk=on_chunk)


class _TypedSink:
    """支持 final/error_code 的新 typed sink；legacy 文本与 typed 事件分开记录。"""

    def __init__(self) -> None:
        self.typed: list[dict[str, object]] = []
        self.legacy: list[str] = []

    def __call__(self, text: str) -> None:
        self.legacy.append(text)

    def write_provider_retry(self, **payload: object) -> bool:
        self.typed.append(dict(payload))
        return True


class _LegacyOnlySink:
    """老实现方：只认原来的五个字段，收到收口参数包会像旧签名一样抛 TypeError。"""

    def __init__(self) -> None:
        self.typed: list[dict[str, object]] = []
        self.legacy: list[str] = []

    def __call__(self, text: str) -> None:
        self.legacy.append(text)

    def write_provider_retry(self, **payload: object) -> bool:
        if set(payload) - {"scope", "attempt", "total", "delay_seconds", "error_type"}:
            raise TypeError("老实现方不认识收口参数包")
        self.typed.append(dict(payload))
        return True


def test_budget_final_event_carries_structured_fields_to_typed_sink(monkeypatch) -> None:
    """到上限时 typed sink 收到 final 事件（final=True + error_code），不再收到普通重试文本。"""
    sink = _TypedSink()
    _run_to_budget(monkeypatch, sink)

    assert sink.legacy == []
    assert sink.typed == [
        {
            "scope": "model_turn",
            "attempt": 1,
            "total": 2,
            "delay_seconds": 10.0,
            "error_type": "ProviderTimeoutError",
        },
        {
            "scope": "model_turn",
            "attempt": 2,
            "total": 2,
            "delay_seconds": 25.0,
            "error_type": "ProviderTimeoutError",
            "params": {"final": True, "error_code": FINAL_CODE},
        },
    ]


@pytest.mark.parametrize("mode", ["raise", "reject"])
def test_typed_sink_failure_falls_back_to_legacy_text(monkeypatch, mode: str) -> None:
    """sink 抛异常或返回 False 时，照旧退回现在的文本回调，文本逐字不变。"""

    class Sink(_TypedSink):
        def write_provider_retry(self, **payload: object) -> bool:
            if mode == "raise":
                raise RuntimeError("display channel down")
            return False

    sink = Sink()
    _run_to_budget(monkeypatch, sink)

    assert sink.typed == []
    assert sink.legacy == [
        (
            "\n[provider_transient_auto_resume attempt=1/2; wait_seconds=10]\n"
            "模型接口临时不可用或被限流，等待 10 秒后自动重试当前模型回合。\n"
            f"error={ORIGINAL_ERROR_TEXT}\n"
        ),
        (
            "\n[provider_transient_auto_resume attempt=2/2; budget_exhausted]\n"
            "模型接口持续不可用，已到自动重试的总时长上限，本轮不再重试。\n"
            f"error={ORIGINAL_ERROR_TEXT}\n"
        ),
    ]


def test_legacy_sink_without_final_fields_falls_back_to_legacy_text(monkeypatch) -> None:
    """不认识 final 字段的老实现方：普通重试照常收 typed，收口事件因未知参数回退文本。"""
    sink = _LegacyOnlySink()
    _run_to_budget(monkeypatch, sink)

    assert sink.typed == [
        {
            "scope": "model_turn",
            "attempt": 1,
            "total": 2,
            "delay_seconds": 10.0,
            "error_type": "ProviderTimeoutError",
        }
    ]
    assert sink.legacy == [
        (
            "\n[provider_transient_auto_resume attempt=2/2; budget_exhausted]\n"
            "模型接口持续不可用，已到自动重试的总时长上限，本轮不再重试。\n"
            f"error={ORIGINAL_ERROR_TEXT}\n"
        )
    ]


def test_gateway_writer_final_event_uses_registered_user_copy_and_tui_projection(tmp_path) -> None:
    """Gateway 流的 final 事件：结构化字段齐全、文案是已登记用户文案，TUI 投影显示同一句。"""
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)

    accepted = writer.write_provider_retry(
        scope="model_turn",
        attempt=2,
        total=2,
        delay_seconds=25.0,
        error_type="ProviderTimeoutError",
        params={"final": True, "error_code": FINAL_CODE},
    )
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert accepted is True and len(rows) == 1
    assert rows[0]["kind"] == "runtime_progress" and rows[0]["verbose_level"] == "full"
    assert rows[0]["text"] == FINAL_TEXT and "秒后自动重连" not in rows[0]["text"]
    assert rows[0]["retry"] == {
        "scope": "model_turn",
        "attempt": 2,
        "total": 2,
        "wait_seconds": 25.0,
        "error_type": "ProviderTimeoutError",
        "final": True,
        "error_code": FINAL_CODE,
    }
    text, _terminal = project_gateway_stream_chunk(rows[0])
    assert text == FINAL_TEXT


def test_normal_retry_event_keeps_legacy_field_set(tmp_path) -> None:
    """正常重连进度不受影响：普通事件一个字段都不加，旧客户端与既有断言保持兼容。"""
    path = tmp_path / "request.chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    writer.write_provider_retry(scope="transport", attempt=1, total=3, delay_seconds=2.0, error_type="URLError")
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["text"] == "模型服务暂时不可用，2 秒后自动重连（连接 1/3）"
    assert rows[0]["retry"] == {
        "scope": "transport",
        "attempt": 1,
        "total": 3,
        "wait_seconds": 2.0,
        "error_type": "URLError",
    }


def test_background_transcript_final_notice_shares_the_same_user_copy() -> None:
    """IM/后台正文的收口提示与 TUI 显示同一句已登记用户文案。"""
    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(agent, thread_id="thread-1", task_id="task-1")

    sink.write_provider_retry(attempt=2, total=2, delay_seconds=25.0, params={"final": True, "error_code": FINAL_CODE})

    events = read_background_transcript_events(agent, thread_id="thread-1", after=0)["events"]
    assert events[-1]["kind"] == "system_message"
    assert events[-1]["payload"]["text"] == FINAL_TEXT
    assert "等待" not in events[-1]["payload"]["text"]


# 扫描产品代码里所有 write_provider_retry 定义；测试替身不算实现方，故排除 tests 目录。
def _provider_retry_definitions() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """返回产品代码里每个 write_provider_retry 函数定义（文件 + AST 节点）。"""
    package_root = Path(__file__).resolve().parents[1]
    production_paths = [
        path for path in sorted(package_root.rglob("*.py"))
        if path.relative_to(package_root).parts[0] != "tests"
    ]
    return [
        (path, node)
        for path in production_paths
        for node in _write_provider_retry_nodes(path)
    ]


def _write_provider_retry_nodes(path: Path) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """从一个模块文件里取出所有名为 write_provider_retry 的函数定义节点。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "write_provider_retry"
    ]


def test_every_provider_retry_implementation_declares_params_explicitly() -> None:
    """回退约定守卫：实现方必须显式声明 params，禁止用 **kwargs 静默收下收口参数。"""
    definitions = _provider_retry_definitions()
    assert definitions, "没有扫到任何 write_provider_retry 实现，守卫本身失效"
    for path, node in definitions:
        names = {argument.arg for argument in [*node.args.args, *node.args.kwonlyargs]}
        assert "params" in names, f"{path.name}: write_provider_retry 必须显式声明 params"
        assert node.args.kwarg is None, f"{path.name}: write_provider_retry 不得用 **kwargs 收下收口参数"
