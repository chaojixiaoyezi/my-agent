# LLM: 这一层是给整个测试套件用的环境能力判定，错了会让"沙箱里跳过"变成"静默不跑"或反过来
#   把能跑的用例全跳掉，所以探测缓存、判定口径和强制开关都要有独立用例钉住。
# 模块用途: 验证沙箱能力探测的缓存、skip/fail 判定和标记驱动的收集期行为。
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.tests import _sandbox_capabilities as caps

REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: 真实探测结果按会话缓存；测试间必须清空，免得前例环境伪装留给后例。
# 函数用途: 每个用例前后清掉探测缓存，避免相互影响。
@pytest.fixture(autouse=True)
def _clear_probe_cache():
    caps.capability_available.cache_clear()
    yield
    caps.capability_available.cache_clear()


def test_probe_result_is_cached_within_a_session(monkeypatch) -> None:
    """探测有副作用（起子进程），同一能力整个会话只探一次。"""

    calls: list[str] = []

    def fake_probe() -> bool:
        calls.append("ps")
        return True

    monkeypatch.setitem(caps._PROBES, "ps", fake_probe)
    caps.capability_available.cache_clear()
    assert caps.capability_available("ps") is True
    assert caps.capability_available("ps") is True
    assert calls == ["ps"], calls


def test_unknown_capability_is_rejected() -> None:
    """未知能力名要直接报错，不能静默当"不可用"。"""

    with pytest.raises(KeyError):
        caps.capability_available("no_such_capability")


def test_missing_capability_skips_by_default(monkeypatch) -> None:
    """能力缺失且没设强制开关时，判定是 skip 且原因写清缺什么、去哪真跑。"""

    monkeypatch.setitem(caps._PROBES, "ps", lambda: False)
    monkeypatch.delenv("MY_AGENT_TEST_REQUIRE_CAPABILITIES", raising=False)
    caps.capability_available.cache_clear()
    gate = caps.capability_gate(["ps"])
    assert gate is not None and gate.startswith("SKIP: ")
    assert "SANDBOX_CAPABILITY_MISSING[ps]" in gate
    assert "具备该能力的平台/非受限环境中真跑" in gate


def test_missing_capability_fails_when_required(monkeypatch) -> None:
    """设了强制开关后，能力缺失必须变成 fail，不能静默跳过。"""

    monkeypatch.setitem(caps._PROBES, "ps", lambda: False)
    monkeypatch.setenv("MY_AGENT_TEST_REQUIRE_CAPABILITIES", "1")
    caps.capability_available.cache_clear()
    gate = caps.capability_gate(["ps"])
    assert gate is not None and gate.startswith("FAIL: ")
    assert "SANDBOX_CAPABILITY_MISSING[ps]" in gate


def test_present_capability_is_not_gated(monkeypatch) -> None:
    """能力齐全时不做任何拦截，用例照常真跑。"""

    monkeypatch.setitem(caps._PROBES, "ps", lambda: True)
    monkeypatch.setenv("MY_AGENT_TEST_REQUIRE_CAPABILITIES", "1")
    caps.capability_available.cache_clear()
    assert caps.capability_gate(["ps"]) is None


def test_require_switch_only_accepts_explicit_one(monkeypatch) -> None:
    """只有明确 =1 才算强制；空串或别的值不能把整条链搞红。"""

    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("MY_AGENT_TEST_REQUIRE_CAPABILITIES", value)
        assert caps.capabilities_required() is False, value
    monkeypatch.setenv("MY_AGENT_TEST_REQUIRE_CAPABILITIES", "1")
    assert caps.capabilities_required() is True


def test_real_probes_only_look_at_structured_results() -> None:
    """真探测必须返回布尔且不抛异常（只看返回码/异常类型，不解析输出文字）。"""

    for name in caps._PROBES:
        caps.capability_available.cache_clear()
        result = caps.capability_available(name)
        assert isinstance(result, bool), name


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        (b" 2468 \n", 0, True),
        (b"", 0, False),
        (b"1357\n", 0, False),
        (b"2468\n", 1, False),
    ],
)
def test_ps_probe_requires_its_own_pid(
    stdout: bytes, returncode: int, expected: bool, monkeypatch
) -> None:
    """ps 的成功码还必须配合仅含本 PID 的结构化输出，防空结果假阳性。"""

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    monkeypatch.setattr(caps.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(caps.os, "getpid", lambda: 2468)
    monkeypatch.setattr(caps.subprocess, "run", run)

    assert caps._probe_ps() is expected
    assert calls[0][0] == ["/bin/ps", "-o", "pid=", "-p", "2468"]


# LLM: 这个子进程测试把探针固定为缺失，验证 pytest 公共 hook 的真实 skip/fail 结果，
#   不以被测函数自行报错冒充能力门生效；写入仅落在 tmp_path。
# 函数用途: 在临时 pytest 项目里验证缺失能力的错误归因和平台 skipif 优先级。
def _run_generated_capability_case(
    case_root: Path,
    source: str,
    *,
    require: bool,
) -> subprocess.CompletedProcess:
    case_root.mkdir(parents=True)
    (case_root / "conftest.py").write_text(
        "from agent_py_agent.tests import _sandbox_capabilities as caps\n"
        "def pytest_configure(config):\n"
        "    caps._PROBES['nested_sandbox_exec'] = lambda: False\n",
        encoding="utf-8",
    )
    test_file = case_root / "test_capability_case.py"
    test_file.write_text(source, encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if require:
        env["MY_AGENT_TEST_REQUIRE_CAPABILITIES"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "agent_py_agent.tests.conftest",
            str(test_file),
            "-q",
            "-rs",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--basetemp",
            str(case_root / "basetemp"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_setup_gate_reports_missing_id_and_platform_skipif_wins(tmp_path: Path) -> None:
    """缺能力失败必须点名能力；显式平台 skipif 必须先于能力门生效。"""

    marked_body = (
        'import pytest\n'
        '@pytest.mark.sandbox_capability("nested_sandbox_exec")\n'
        'def test_marked_body():\n'
        '    raise AssertionError("TEST_BODY_EXECUTED")\n'
    )
    default_run = _run_generated_capability_case(
        tmp_path / "default", marked_body, require=False
    )
    assert default_run.returncode == 0, default_run.stdout[-1000:]
    assert "nested_sandbox_exec" in default_run.stdout, default_run.stdout[-1000:]

    required_run = _run_generated_capability_case(
        tmp_path / "required", marked_body, require=True
    )
    assert required_run.returncode != 0, required_run.stdout[-1000:]
    assert "SANDBOX_CAPABILITY_MISSING[nested_sandbox_exec]" in required_run.stdout, (
        required_run.stdout[-1000:]
    )
    assert "TEST_BODY_EXECUTED" not in required_run.stdout

    platform_marked_body = (
        'import pytest\n'
        '@pytest.mark.skipif(True, reason="模拟不支持该平台")\n'
        '@pytest.mark.sandbox_capability("nested_sandbox_exec")\n'
        'def test_platform_body():\n'
        '    raise AssertionError("PLATFORM_BODY_EXECUTED")\n'
    )
    platform_run = _run_generated_capability_case(
        tmp_path / "platform", platform_marked_body, require=True
    )
    assert platform_run.returncode == 0, platform_run.stdout[-1000:]
    assert "1 skipped" in platform_run.stdout, platform_run.stdout[-1000:]
    assert "PLATFORM_BODY_EXECUTED" not in platform_run.stdout


# —— 端到端：拿一个真实被标记的用例，验证 skip / fail 两种模式 ——

# 真实被标记的用例；用它确保"标记在真实文件上确实生效"，而不是只验证判定函数。
_MARKED_TARGET = (
    "agent_py_agent/tests/test_gateway_port_deny.py"
    "::test_full_access_also_blocks_gateway_port"
)


# LLM: 子 pytest 必须走仓库 conftest 的真实 runtest_setup hook；保留最小环境避免继承运行时状态。
# 函数用途: 用受控环境跑一条真实被标记的用例，返回完成结果。
def _run_marked(tmp_path: Path, *, require: bool) -> subprocess.CompletedProcess:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if require:
        env["MY_AGENT_TEST_REQUIRE_CAPABILITIES"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", _MARKED_TARGET,
         "-q", "-rs", "-rfE", "--tb=short", "-p", "no:cacheprovider", "-o", "addopts=",
         "--basetemp", str(tmp_path / "bt")],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=300,
    )


def test_marked_case_skips_or_fails_by_environment(tmp_path) -> None:
    """带标记的用例：默认模式跳过，强制模式失败——两种都必须真的发生。"""

    caps.capability_available.cache_clear()
    if caps.capability_available("nested_sandbox_exec"):
        pytest.skip("本机嵌套 sandbox-exec 能力齐全，无法验证缺失分支")

    default_run = _run_marked(tmp_path, require=False)
    assert default_run.returncode == 0, default_run.stdout[-800:]
    # -q 下跳过显示为 s；只断言"这条被跳过且整体仍然绿"，不绑定 pytest 的摘要文案。
    assert default_run.stdout.lstrip().startswith("s"), default_run.stdout[-800:]
    assert "passed" not in default_run.stdout, default_run.stdout[-800:]

    if sys.platform != "darwin":
        required_run = _run_marked(tmp_path / "req", require=True)
        assert required_run.returncode == 0, required_run.stdout[-800:]
        assert required_run.stdout.lstrip().startswith("s"), required_run.stdout[-800:]
        return

    required_run = _run_marked(tmp_path / "req", require=True)
    assert required_run.returncode != 0, required_run.stdout[-800:]
    assert "SANDBOX_CAPABILITY_MISSING[nested_sandbox_exec]" in required_run.stdout, (
        required_run.stdout[-800:]
    )
