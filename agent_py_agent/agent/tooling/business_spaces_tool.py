"""业务与原生工具共用的模型 schema 子集投影辅助函数。"""
from __future__ import annotations


def _model_projection(value):
    # 原ToolModelSpec只接受有限子集。仅展示层将uniqueItems保存在x扩展；完整原schema
    # 在validate_request及ActionPolicy业务门原样执行，因此重复值仍在effect/claim前拒绝。
    if isinstance(value, dict):
        return {('x-business-unique-items' if key == 'uniqueItems' else key): _model_projection(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_model_projection(item) for item in value]
    return value
