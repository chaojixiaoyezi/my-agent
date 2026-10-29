# LLM: 仅用内存 facet 驱动聚合器，覆盖数据相同与不同的机器结构，不读取真实用户目录。
# 模块用途: 回归 DoctorService all 投影和原视图兼容。
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.user_space.home_doctor import DoctorService


# LLM: 每次返回独立字典，避免 pop 污染后续报告；没有文件和服务副作用。
# 函数用途: 为聚合测试装配 runtime/memory 两个可控视图。
def _service(equal):
    service = DoctorService(None)
    runtime = {"ok": True, "config": {"a": 1}, "routing": {"route_count": 0},
               "archive": {"hook": {}}, "warnings": [], "home": {"root": "test"}}
    memory = {"ok": True, "config": {"a": 1 if equal else 2},
              "routing": {"route_count": 0 if equal else 1},
              "archive": {"hook": {} if equal else {"exists": True}},
              "config_warnings": [] if equal else [{"message": "warning"}], "candidate": {"count": 0}}
    service._runtime = Mock(side_effect=lambda args: deepcopy(runtime))
    service._memory = Mock(side_effect=lambda args: deepcopy(memory))
    return service, runtime, memory


# LLM: common 字段固定 runtime/memory 键，即使数据相等也不能退化；修改公开结果不可污染后续报告。
# 函数用途: 钉住 all 报告的字段形状与返回值隔离。
@pytest.mark.parametrize("equal", [True, False])
def test_all_common_shape_is_stable(equal):
    service, runtime, memory = _service(equal)
    report = service.report(SimpleNamespace(), scope="all")
    assert report["ok"] is True
    assert set(report["common"]) == {"config", "routing", "archive", "warnings"}
    for field in report["common"]:
        assert set(report["common"][field]) == {"runtime", "memory"}
        assert report["common"][field]["runtime"] == runtime[field]
        assert report["common"][field]["memory"] == memory["config_warnings" if field == "warnings" else field]
    report["common"]["config"]["runtime"]["a"] = 999
    assert service.report(SimpleNamespace(), scope="all")["common"]["config"]["runtime"]["a"] == 1


# LLM: all 的协议调整不得改变 legacy 平铺 runtime 或默认 memory 输出，也不额外调用另一视图。
# 函数用途: 对原有诊断视图作逐字段兼容检查。
def test_original_views_stay_compatible():
    service, runtime, memory = _service(False)
    assert service.report(SimpleNamespace(), scope="memory") == memory
    service._runtime.assert_not_called()
    assert service.report(SimpleNamespace(), scope="runtime") == {"ok": True, "scope": "runtime", "runtime": runtime}
    assert service.report(SimpleNamespace(), legacy=True) == runtime
