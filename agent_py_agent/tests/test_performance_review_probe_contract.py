# LLM: 探针测试仅使用进程内函数或tmp_path样本，失败不转为零值成功，不触及真实用户状态。
# 模块用途: 验证性能探针的观测范围、失败合同和脱敏输入摘要。
from __future__ import annotations

import json
import tracemalloc

import pytest

from scripts.benchmark_config_policy import benchmark
from scripts.performance_review_probe import main, measure_call


# LLM: 每次调用都是真实执行计数，不以warmup冒充样本。
# 函数用途: 确认样本数、预热数与实际调用次数一致。
def test_probe_samples_measure_actual_calls():
    calls = []
    result = measure_call(lambda: calls.append(1), iterations=2, warmup=1)
    assert len(calls) == 3
    assert len(result["samples"]) == 2
    assert result["median_wall_seconds"] >= 0
    assert not tracemalloc.is_tracing()


# LLM: 操作失败不得返回空成功报告，跟踪器必须收回。
# 函数用途: 保证真实异常传播且测量状态清理。
def test_probe_failure_is_not_zero_success():
    with pytest.raises(ZeroDivisionError):
        measure_call(lambda: 1 / 0, iterations=1, warmup=0)
    assert not tracemalloc.is_tracing()


# LLM: 超限和空测量都在业务执行前拒绝。
# 函数用途: 验证有界输入约束。
@pytest.mark.parametrize("count", [0, -1, 501, True, 1.5, "2"])
def test_probe_rejects_invalid_count(count):
    with pytest.raises(ValueError):
        measure_call(lambda: None, iterations=count)


# LLM: 预热同样是严格整数且有上界，不以布尔或小数隐式转换放宽测量输入。
# 函数用途: 验证预热参数在执行操作前拒绝不合法类型。
@pytest.mark.parametrize("warmup", [-1, 26, True, 0.5, "1"])
def test_probe_rejects_invalid_warmup(warmup):
    with pytest.raises(ValueError):
        measure_call(lambda: None, warmup=warmup)


# LLM: CLI失败只输出稳定类型，不回显异常中的配置或凭据正文。
# 函数用途: 验证失败退出码与单JSON输出。
def test_probe_cli_failure_is_structured(capsys):
    assert main(["--iterations", "0", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["samples"] == []


# LLM: 只在测试临时目录读配置；摘要不能泄露配置值。
# 函数用途: 确认正式配置加载探针成功且不输出秘密。
def test_config_probe_reads_without_mutation(tmp_path):
    path = tmp_path / "sample.yaml"
    path.write_text('model_backend: "echo"\napi_key: "test-private-value"\n')
    before = path.read_bytes()
    result = benchmark(path, iterations=1, warmup=0)
    assert result["ok"] and len(result["samples"]) == 1
    assert path.read_bytes() == before
    assert "test-private-value" not in json.dumps(result)
    assert "端到端" in result["provenance"]["limitation"]
