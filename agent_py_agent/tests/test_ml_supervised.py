
from __future__ import annotations

"""监督路训练模型单测 —— M3-5(纯内置逻辑回归 + 影子对比)。

LR 在标注数据上学会分类(高危特征→威胁,低危→良性);影子模式对比确定性 baseline;持久化;工具端到端。
纯 Python 梯度下降,无 numpy/sklearn。
"""

from pathlib import Path

from agent_py_agent.agent.ml_engine import engine, supervised_model
from agent_py_agent.agent.ml_engine.models import MLFeatureVector


def _feat(severity: int, count: int, fan_out: int = 0, anomaly: float = 0.0) -> MLFeatureVector:
    cpm = float(count)
    return MLFeatureVector("c", "d", "f", "alert", severity, count, 0.8, 60.0, cpm, fan_out, fan_out, 0.0, anomaly)


def _threat() -> MLFeatureVector:
    return _feat(5, 500, fan_out=50, anomaly=0.9)


def _benign() -> MLFeatureVector:
    return _feat(1, 2, fan_out=0, anomaly=0.0)


def test_lr_learns_to_classify() -> None:
    """LR 在合成数据上学会:高危特征→威胁(预测高),低危→良性(预测低)。"""
    samples = [(_threat(), 1) for _ in range(20)] + [(_benign(), 0) for _ in range(20)]
    model = supervised_model.train(samples, epochs=300)
    assert model.predict(_threat()) > 0.6  # 威胁高分
    assert model.predict(_benign()) < 0.4  # 良性低分


def test_shadow_compare_accuracy() -> None:
    """影子对比:LR vs 确定性 baseline 在标注样本上准确率;LR 学得好(≥0.8)。"""
    samples = [(_threat(), 1) for _ in range(15)] + [(_benign(), 0) for _ in range(15)]
    model = supervised_model.train(samples, epochs=300)
    shadow = supervised_model.shadow_compare(model, engine.supervised_baseline, samples)
    assert shadow.samples == 30 and shadow.model_accuracy >= 0.8


def test_model_persist(tmp_path: Path) -> None:
    samples = [(_threat(), 1) for _ in range(10)] + [(_benign(), 0) for _ in range(10)]
    model = supervised_model.train(samples, epochs=100)
    supervised_model.save_model(tmp_path, model)
    loaded = supervised_model.load_model(tmp_path)
    assert loaded is not None
    assert abs(loaded.predict(_threat()) - model.predict(_threat())) < 1e-6


def test_load_missing_model(tmp_path: Path) -> None:
    assert supervised_model.load_model(tmp_path) is None


def test_predict_contract_same_as_baseline() -> None:
    """predict 与 _supervised_risk 同契约(feature→0-1 float),可无缝替换。"""
    model = supervised_model.train([(_threat(), 1), (_benign(), 0)], epochs=50)
    risk = model.predict(_threat())
    assert isinstance(risk, float) and 0.0 <= risk <= 1.0


def test_log_ml_label_train_end_to_end(tmp_path: Path) -> None:
    """工具:候选 + 标注 → log_ml_label(train=true) 顺便训练 + 影子(训练合并进标注,不单列工具避免膨胀)。"""
    from agent_py_agent.agent.tooling.log_ops.tools import _store
    from agent_py_agent.agent.tooling.log_ops.tools_ml import LogMlLabelTool

    store = _store(tmp_path, "default")
    store.ensure_dirs()
    threat = [{"fingerprint": f"t{i}", "source_id": "api1", "entities": {"ip": "1.1.1.1"}, "detected_at": 1000.0, "matched_rules": ["sql_injection"], "severity": "high", "anomaly_score": 0.9} for i in range(25)]
    benign = [{"fingerprint": f"b{i}", "source_id": "api2", "entities": {"ip": "2.2.2.2"}, "detected_at": 1000.0, "matched_rules": ["xss"], "severity": "low"} for i in range(25)]
    store.append_candidates(threat + benign)
    LogMlLabelTool(tmp_path).execute({"cluster_id": "api1:sql_injection", "outcome": "attempt"})
    result = LogMlLabelTool(tmp_path).execute({"cluster_id": "api2:xss", "outcome": "other", "train": True})
    assert result.ok
    training = result.result_envelope["training"]
    assert training["trained"] and training["samples"] == 2  # 2 个有标注的簇配对


def test_train_robust_to_dirty_features() -> None:
    """脏特征(NaN/inf/负 count,来自日志解析错误、上游脏数据或 LLM 幻觉)不能崩 log1p、不能把整个 LR
    模型权重污染成 NaN(单个脏样本毁掉所有预测的数值投毒;回归:dogfooding 压 ML 引擎逮到)。"""
    import math

    dirty_count = MLFeatureVector("c", "d", "f", "alert", 1, -5, 0.8, 60.0, 5.0, 5, 5, 0.0, 0.0)  # count=-5 原 log1p ValueError
    dirty_nan = MLFeatureVector("c", "d", "f", "alert", 1, 10, float("nan"), 60.0, 5.0, 5, 5, 0.0, float("nan"))
    dirty_inf = MLFeatureVector("c", "d", "f", "alert", float("inf"), 10, 0.8, 60.0, 5.0, 5, 5, 0.0, 0.0)
    samples = [(dirty_count, 1), (dirty_nan, 0), (dirty_inf, 1), (_threat(), 1), (_benign(), 0)]
    model = supervised_model.train(samples, epochs=50)  # 不崩
    assert all(math.isfinite(w) for w in model.weights.values()) and math.isfinite(model.bias)  # 脏样本不污染权重
    assert math.isfinite(model.predict(_threat()))  # 预测仍有限
    norm = supervised_model._normalize(MLFeatureVector("c", "d", "f", "alert", -100, -2, 5.0, 60.0, 999.0, 999, 999, 0.0, -3.0))
    assert all(0.0 <= v <= 1.0 for v in norm.values())  # 负/超界特征全夹到合法 [0,1] 区间
