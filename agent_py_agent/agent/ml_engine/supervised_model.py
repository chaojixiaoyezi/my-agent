
from __future__ import annotations

"""监督路训练模型(M3-5)——纯内置轻量逻辑回归,吃 outcome 标注学"特征→成功攻击"的非线性关系。

监督路从确定性加权升级:确定性公式靠人定权重(0.4/0.2/...);LR 从 agent 标注的 outcome(success/attempt=
威胁=1,failure/other=良性=0)自己学每个特征的权重。**纯 Python 梯度下降,无 numpy/sklearn**(符合"纯内置
不依赖外部 ML 服务")。影子模式:训练后与确定性 baseline 在标注样本上比准确率,**LR 更准才切**(契约
feature→supervised_risk 不变,predict 可无缝替换 _supervised_risk)。冷启动标注少时 LR 不靠谱→影子挡住不切。
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import MLFeatureVector

# 参与训练的数值特征(从 MLFeatureVector 取并归一化到 ~0–1,LR 收敛友好)
_FEATURES = ("severity", "count", "confidence", "count_per_minute", "cardinality", "fan_out", "statistical_anomaly")
_MODEL_FILENAME = "ml_supervised_model.json"


def _sigmoid(z: float) -> float:
    if z <= -30:
        return 0.0
    if z >= 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _clamp01(x: float) -> float:
    """脏特征(NaN/inf/负/超界,来自日志解析错误、上游脏数据或 LLM 幻觉)夹到 [0,1]:否则单个 NaN/inf
    特征会经梯度下降把整个 LR 模型权重/偏置污染成 NaN、毁掉所有预测(数值投毒)。"""
    if math.isnan(x):
        return 0.0
    return max(0.0, min(1.0, x))  # ±inf 被 max/min 自然夹到 [0,1]


def _normalize(feature: MLFeatureVector) -> dict[str, float]:
    """MLFeatureVector → 归一化特征 dict(各特征夹到 0–1)。全经 _clamp01 防脏值污染模型;count 先确保
    有限非负,否则 log1p 对 count≤-1 直接 ValueError(math domain error)崩。"""
    count = feature.count if math.isfinite(feature.count) and feature.count >= 0 else 0.0
    return {
        "severity": _clamp01(feature.severity / 5),
        "count": _clamp01(math.log1p(count) / math.log1p(1000)),
        "confidence": _clamp01(feature.confidence),
        "count_per_minute": _clamp01(feature.count_per_minute / 50),
        "cardinality": _clamp01(feature.cardinality / 100),
        "fan_out": _clamp01(feature.fan_out / 50),
        "statistical_anomaly": _clamp01(feature.statistical_anomaly),
    }


@dataclass
class LogisticModel:
    """轻量逻辑回归:每特征 weight + bias。预测 sigmoid(w·x+b) → supervised_risk(0–1)。"""

    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    trained_samples: int = 0

    def _z(self, norm: dict[str, float]) -> float:
        return self.bias + sum(self.weights.get(k, 0.0) * norm.get(k, 0.0) for k in _FEATURES)

    def predict(self, feature: MLFeatureVector) -> float:
        """契约同 _supervised_risk:feature → 风险分(0–1)。可无缝替换确定性监督路。"""
        return round(_sigmoid(self._z(_normalize(feature))), 4)

    def to_dict(self) -> dict[str, Any]:
        return {"weights": self.weights, "bias": self.bias, "trained_samples": self.trained_samples}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LogisticModel":
        return cls(weights={k: float(v) for k, v in (data.get("weights") or {}).items()},
                   bias=float(data.get("bias") or 0.0), trained_samples=int(data.get("trained_samples") or 0))


@dataclass(frozen=True)
class ShadowResult:
    """影子对比:LR vs 确定性 baseline 在标注样本上的准确率。"""

    model_accuracy: float
    baseline_accuracy: float
    samples: int
    model_better: bool


def _train_epoch(model: LogisticModel, norm_samples: list[tuple[dict[str, float], int]], lr: float) -> None:
    for norm, label in norm_samples:
        error = _sigmoid(model._z(norm)) - label
        for key in _FEATURES:
            model.weights[key] -= lr * error * norm.get(key, 0.0)
        model.bias -= lr * error


def train(samples: list[tuple[MLFeatureVector, int]], epochs: int = 300, lr: float = 0.3) -> LogisticModel:
    """梯度下降训练 LR。samples=[(feature, label)],label:威胁=1(success/attempt),良性=0。"""
    norm_samples = [(_normalize(feature), label) for feature, label in samples]
    model = LogisticModel(weights={k: 0.0 for k in _FEATURES})
    for _ in range(max(1, epochs)):
        _train_epoch(model, norm_samples, lr)
    model.trained_samples = len(samples)
    return model


def shadow_compare(model: LogisticModel, baseline_fn: Any, samples: list[tuple[MLFeatureVector, int]]) -> ShadowResult:
    """影子对比:LR 模型 vs 确定性 baseline 在标注样本上准确率(预测 ≥0.5 算威胁,比命中率)。"""
    n = len(samples)
    if not n:
        return ShadowResult(0.0, 0.0, 0, False)
    model_correct = sum(1 for feature, label in samples if (model.predict(feature) >= 0.5) == bool(label))
    base_correct = sum(1 for feature, label in samples if (baseline_fn(feature) >= 0.5) == bool(label))
    model_acc, base_acc = round(model_correct / n, 3), round(base_correct / n, 3)
    return ShadowResult(model_acc, base_acc, n, model_acc > base_acc)


def model_path(store_root: Path) -> Path:
    return Path(store_root) / _MODEL_FILENAME


def save_model(store_root: Path, model: LogisticModel) -> None:
    path = model_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")


def load_model(store_root: Path) -> LogisticModel | None:
    """读已训练的监督模型(没有则 None,引擎回退确定性 baseline)。"""
    path = model_path(store_root)
    if not path.exists():
        return None
    try:
        return LogisticModel.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


__all__ = [
    "LogisticModel",
    "ShadowResult",
    "train",
    "shadow_compare",
    "model_path",
    "save_model",
    "load_model",
]
