# LLM: 仅供发行布局测试复核真实解释器和模块来源，遵守 acceptance_common.business_origins 的 pyc 权威边界。
# 模块用途: 区分严格源码 cwd 与隔离 runtime，避免把开发源码或另一安装版误算作包内行为。
import sys
from pathlib import Path


# LLM: 输入是当前或真实子进程采集的 __file__/prefix/cwd；不能用宽 cwd 包容任意安装版，也不能接受 runtime 内源码。
# 函数用途: 校验业务来源属于本次源码树或同解释器的隔离纯 pyc runtime，返回已验证的路径。
def assert_business_origins(facts, *, cwd=None):
    expected_cwd = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    assert Path(facts["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert Path(facts["cwd"]).resolve() == expected_cwd
    origins = facts["modules"]
    assert origins
    expected_suffix = Path(sys.modules["agent_py_agent"].__file__).suffix
    assert expected_suffix in {".py", ".pyc"}
    paths = {}
    for name, filename in origins.items():
        assert name in {"agent", "agent_py_agent"} or name.startswith(("agent.", "agent_py_agent."))
        assert not name.startswith("agent_py_agent.tests")
        origin = Path(filename).resolve(strict=True)
        assert origin.suffix == expected_suffix, "子进程与当前业务包的源码/pyc模式必须一致"
        if origin.suffix == ".py":
            assert origin.is_relative_to(expected_cwd / "agent_py_agent")
        else:
            assert origin.suffix == ".pyc" and facts["isolated"]
            assert origin.is_relative_to(Path(sys.prefix).resolve())
            assert not origin.with_suffix(".py").exists()
        paths[name] = origin
    return paths


# LLM: 只读当前模块的加载事实，不读源码、配置或用户资料；子进程使用同一结构自行采集后由父断言。
# 函数用途: 给单模块测试提供与发行 runner 一致的真实来源检查。
def assert_business_module(module):
    return assert_business_origins({
        "prefix": sys.prefix, "cwd": str(Path.cwd()), "isolated": bool(sys.flags.isolated),
        "modules": {module.__name__: module.__file__},
    })[module.__name__]


# LLM: 子进程执行后的真实 sys.modules 快照只供测试证据，不执行业务或给来源授信，完整结果仍由父严格核对。
# 函数用途: 返回采集代码，避免子进程为了查来源再从测试目录导入额外业务。
def child_origin_capture(output_expression):
    return (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "facts = {'prefix': sys.prefix, 'cwd': os.getcwd(), 'isolated': bool(sys.flags.isolated), "
        "'modules': {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items() "
        "if (name in {'agent', 'agent_py_agent'} or name.startswith(('agent.', 'agent_py_agent.'))) "
        "and not name.startswith('agent_py_agent.tests') and getattr(module, '__file__', None)}}\n"
        + output_expression + "\n"
    )
